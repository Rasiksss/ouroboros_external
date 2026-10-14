from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.registry import TOOL_BODIES, concurrency_budgets
from new_core.llm.types import ToolCall, ToolCallFunction
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.agent.loop_adapters import build_tool_call_executor
from ouroboros.agent_runtime.cancellation import EventCancellation
from ouroboros.policy import safety
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import collecting_event_sink, make_catalog, make_tool_ctx, static_tool_provider


@parametrize_from_dict({"occupied": {"grant_on_stop": False}, "granted_during_stop": {"grant_on_stop": True}})
def test_stop_ends_tool_budget_wait_without_dispatch(tmp_path, grant_on_stop) -> None:
    """A cancelled waiter exits even while another call keeps its tool-body grant."""
    entered = threading.Event()
    cancellation = EventCancellation()

    class ObservedBudget(InProcessBudget):
        def acquire(self, budget, *, holder, **kwargs):
            if holder != "occupied":
                entered.set()
            lease = super().acquire(budget, holder=holder, **kwargs)
            if holder != "occupied" and lease is not None and grant_on_stop:
                cancellation.request_stop()
            return lease

    budgets = ObservedBudget({TOOL_BODIES: 1})
    held = budgets.acquire(TOOL_BODIES, holder="occupied", ttl_sec=60)
    concurrency_budgets.initialize(budgets)
    calls = []
    entry = ToolEntry(
        name="data_read",
        schema=ToolSchema(name="data_read", description="d", parameters={}),
        handler=lambda _ctx, **_kwargs: calls.append("started"),
        parallel_safe=True,
    )
    executor = build_tool_call_executor(
        catalog=make_catalog([static_tool_provider(entry)]),
        events=collecting_event_sink(),
        confirmation_gate=False,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            executor.execute,
            [ToolCall(id=f"call-{i}", function=ToolCallFunction(name="data_read", arguments="{}")) for i in range(2)],
            ctx=make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path),
            task_id="stopped",
            cancellation=cancellation,
            decisions={},
        )
        assert entered.wait(2)
        if grant_on_stop:
            budgets.release(held)
        else:
            cancellation.request_stop()
        assert future.result(timeout=2) == []
        assert calls == []
        assert budgets.in_use(TOOL_BODIES) == (0 if grant_on_stop else 1)
    finally:
        budgets.release(held)
        pool.shutdown(wait=True)
        concurrency_budgets.reset_for_tests()


def test_a_stop_during_one_tool_prevents_the_next_tool_from_starting(tmp_path, monkeypatch) -> None:
    cancellation = EventCancellation()
    calls: list[int] = []
    observed: list[tuple[bool, bool]] = []

    def run(ctx, **_kwargs):
        calls.append(len(calls) + 1)
        before = ctx.stop_requested()
        cancellation.request_stop()
        observed.append((before, ctx.stop_requested()))
        return "done"

    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    entry = ToolEntry(
        name="data_read",
        schema=ToolSchema(name="data_read", description="d", parameters={}),
        handler=run,
        parallel_safe=False,
    )
    executor = build_tool_call_executor(
        catalog=make_catalog([static_tool_provider(entry)]),
        events=collecting_event_sink(),
        confirmation_gate=False,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    tool_calls = [
        ToolCall(id=f"call-{index}", function=ToolCallFunction(name="data_read", arguments=json.dumps({})))
        for index in range(2)
    ]

    results = executor.execute(
        tool_calls,
        ctx=make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path),
        task_id="task-stopped",
        cancellation=cancellation,
        decisions={},
    )

    assert calls == [1]
    assert observed == [(False, True)]
    assert [result["tool_call_id"] for result in results] == ["call-0"]


def test_stop_before_tool_dispatch_returns_without_starting_a_body(tmp_path) -> None:
    """A stopped sequential call returns no result instead of awaiting None."""
    cancellation = EventCancellation()
    cancellation.request_stop()
    calls = []
    entry = ToolEntry(
        name="data_read",
        schema=ToolSchema(name="data_read", description="d", parameters={}),
        handler=lambda _ctx, **_kwargs: calls.append("started"),
        parallel_safe=False,
    )
    executor = build_tool_call_executor(
        catalog=make_catalog([static_tool_provider(entry)]),
        events=collecting_event_sink(),
        confirmation_gate=False,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    results = executor.execute(
        [ToolCall(id="call", function=ToolCallFunction(name="data_read", arguments="{}"))],
        ctx=make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path),
        task_id="stopped",
        cancellation=cancellation,
        decisions={},
    )
    assert results == []
    assert calls == []
