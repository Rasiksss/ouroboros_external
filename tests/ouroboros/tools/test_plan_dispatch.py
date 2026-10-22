"""Execution authority through the real dispatch, plan repository and subtask tool."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from new_core.tools.providers.builtin import BuiltinToolProvider
from ouroboros.agent_events.interfaces import SubtaskRequested
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.dispatch import execute_tool
from ouroboros.tools.events import PublishedToolEvents
from ouroboros.tools.service.task_planning import task_plan
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.providers import RecordingProvider
from tests.helpers.shared import make_catalog, run_tool
from tests.helpers.task_plans import make_plan_tool_ctx


@pytest.fixture(autouse=True)
def plan_key(monkeypatch):
    monkeypatch.setenv(
        "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
    )


def executor_provider():
    provider = RecordingProvider("execute_report", "ext:reports", skips_llm_safety=True, safety_policy="skip")
    provider._desc = replace(
        provider._desc, capability_class="mutative", schema=replace(provider._desc.schema, plan_executor=True)
    )
    return provider


def binding(revision=1):
    return {"revision": revision, "step_id": "S1", "attempt_id": "build-1"}


def test_unbound_executor_is_refused_before_provider_runs(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path, kind="scenario")
    provider = executor_provider()
    result = run_tool(make_catalog([provider]), ctx, "execute_report", {})
    assert "PLAN_BINDING_REQUIRED" in result
    assert provider.calls == []


def test_stale_binding_is_refused_without_starting_execution(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path, kind="scenario")
    provider = executor_provider()
    result = run_tool(make_catalog([provider]), ctx, "execute_report", {"_plan": binding(9)})
    assert "PLAN_EXECUTION_REFUSED" in result
    assert provider.calls == []
    assert json.loads(task_plan(ctx, "read"))["steps"]["S1"]["status"] == "pending"


def test_bound_call_starts_named_attempt_and_keeps_result_digest(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path, kind="scenario")
    provider = executor_provider()
    assert (
        run_tool(make_catalog([provider]), ctx, "execute_report", {"value": 7, "_plan": binding()})
        == "ran execute_report"
    )
    assert provider.calls[0][2] == {"value": 7}
    state = json.loads(task_plan(ctx, "read"))
    assert state["steps"]["S1"]["status"] == "running"
    assert state["steps"]["S1"]["attempt_id"] == "build-1"
    (call,) = state["calls"]
    assert call["tool_name"] == "execute_report"
    assert call["status"] == "returned"
    assert call["result_ref"].startswith("sha256:")


def test_schedule_requires_binding_and_tracks_live_child_before_review(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    sink = RecordingAgentEvents()
    ctx.events = PublishedToolEvents(sink, task_id="root", scope=ctx.scope, audit_event_repository=None)
    catalog = make_catalog([BuiltinToolProvider(modules=("service.orchestration",))])
    assert "PLAN_BINDING_REQUIRED" in run_tool(catalog, ctx, "schedule_task", {"description": "Build workbook"})
    assert sink.published == []
    result = run_tool(catalog, ctx, "schedule_task", {"description": "Build workbook", "_plan": binding()})
    assert result.startswith("Task request queued")
    (request,) = sink.of_type(SubtaskRequested)
    state = json.loads(task_plan(ctx, "read"))
    assert state["calls"][0]["child_task_id"] == request.task_id
    assert state["steps"]["S1"]["status"] == "running"
    refused = task_plan(
        ctx,
        "step",
        {
            "expected_version": state["version"],
            "step_id": "S1",
            "status": "blocked",
            "attempt_id": "build-1",
            "reason": "Stop",
        },
    )
    assert "executor" in refused.lower()
    assert task_plan(
        ctx, "finish", {"expected_version": state["version"], "status": "partial", "reason": "Stop"}
    ).startswith("ERROR:")
    runtime_persistence.current.tasks_repo.complete_task(request.task_id, result="Workbook complete")
    assert not task_plan(
        ctx,
        "step",
        {
            "expected_version": state["version"],
            "step_id": "S1",
            "status": "awaiting_review",
            "attempt_id": "build-1",
            "result_refs": ["file:xlsx@v1"],
        },
    ).startswith("ERROR:")


def test_cancellation_is_published_before_root_returns(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    sink = RecordingAgentEvents()
    ctx.events = PublishedToolEvents(sink, task_id="root", scope=ctx.scope, audit_event_repository=None)
    catalog = make_catalog([BuiltinToolProvider(modules=("service.orchestration",))])
    assert "Cancel requested" in run_tool(catalog, ctx, "cancel_task", {"task_id": "planner"})
    assert [type(event).__name__ for event in sink.published] == ["TaskCancellationRequested"]
    assert ctx.pending_events == []


def test_independent_steps_can_start_concurrently_without_losing_receipts(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path, kind="scenario", steps=2)
    provider = executor_provider()
    catalog = make_catalog([provider])

    def run(index):
        return run_tool(catalog, ctx, "execute_report", {"_plan": {**binding(), "step_id": f"S{index}"}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, [1, 2])) == ["ran execute_report", "ran execute_report"]
    state = json.loads(task_plan(ctx, "read"))
    assert {call["step_id"] for call in state["calls"]} == {"S1", "S2"}
    assert all(call["status"] == "returned" for call in state["calls"])


def test_lifecycle_close_waits_for_the_bound_child(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    closer = RecordingProvider("close_report", "ext:reports", skips_llm_safety=True, safety_policy="skip")
    closer._desc = replace(closer._desc, schema=replace(closer._desc.schema, plan_closer=True))
    catalog = make_catalog([BuiltinToolProvider(modules=("service.orchestration",)), closer])
    assert "Task request queued" in run_tool(
        catalog, ctx, "schedule_task", {"description": "Build workbook", "_plan": binding()}
    )
    assert "PLAN_EXECUTION_REFUSED" in run_tool(catalog, ctx, "close_report", {})
    assert closer.calls == []
    state = json.loads(task_plan(ctx, "read"))
    runtime_persistence.current.tasks_repo.complete_task(state["calls"][0]["child_task_id"], result="done")
    assert run_tool(catalog, ctx, "close_report", {}) == "ran close_report"


def test_executor_exception_keeps_a_failed_receipt_for_root_review(tmp_path):
    class FailingProvider(RecordingProvider):
        def execute(self, ctx, name, args):
            raise RuntimeError("executor failed")

    ctx = make_plan_tool_ctx(tmp_path, kind="scenario")
    provider = FailingProvider("execute_report", "ext:reports", skips_llm_safety=True, safety_policy="skip")
    provider._desc = replace(provider._desc, schema=replace(provider._desc.schema, plan_executor=True))
    with pytest.raises(RuntimeError, match="executor failed"):
        run_tool(make_catalog([provider]), ctx, "execute_report", {"_plan": binding()})
    state = json.loads(task_plan(ctx, "read"))
    assert state["calls"][0]["status"] == "failed"
    assert state["steps"]["S1"]["status"] == "running"


def test_cancellation_without_transport_retains_fallback_event(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    catalog = make_catalog([BuiltinToolProvider(modules=("service.orchestration",))])
    assert "Cancel requested" in run_tool(catalog, ctx, "cancel_task", {"task_id": "planner"})
    assert [type(event).__name__ for event in ctx.pending_events] == ["TaskCancellationRequested"]


def test_closer_reserves_exclusion_before_its_provider_runs(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class ClosingProvider(RecordingProvider):
        def execute(self, ctx, name, args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release closer")
            return "closed"

    ctx = make_plan_tool_ctx(tmp_path, kind="scenario")
    closer = ClosingProvider("close_report", "ext:reports", skips_llm_safety=True, safety_policy="skip")
    closer._desc = replace(closer._desc, schema=replace(closer._desc.schema, plan_closer=True))
    executor = executor_provider()
    catalog = make_catalog([closer, executor])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run_tool, catalog, ctx, "close_report", {})
        try:
            assert entered.wait(10)
            result = run_tool(catalog, ctx, "execute_report", {"_plan": binding()})
            assert "PLAN_EXECUTION_REFUSED" in result
            assert executor.calls == []
        finally:
            release.set()
        assert pending.result() == "closed"


def test_cancelled_wait_does_not_release_a_still_running_tool_body(tmp_path):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    class SlowProvider(RecordingProvider):
        def execute(self, ctx, name, args):
            entered.set()
            try:
                if not release.wait(10):
                    raise RuntimeError("test did not release body")
                return "late result"
            finally:
                ended.set()

    ctx = make_plan_tool_ctx(tmp_path, kind="scenario")
    provider = SlowProvider("execute_report", "ext:reports", skips_llm_safety=True, safety_policy="skip")
    provider._desc = replace(provider._desc, schema=replace(provider._desc.schema, plan_executor=True))

    async def cancel_wait():
        pending = asyncio.create_task(
            execute_tool(make_catalog([provider]), ctx, "execute_report", {"_plan": binding()}, origin="ext:reports")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not ended.is_set()
            state = json.loads(task_plan(ctx, "read"))
            assert state["calls"][0]["status"] == "running"
            refused = task_plan(
                ctx,
                "step",
                {
                    "expected_version": state["version"],
                    "step_id": "S1",
                    "status": "blocked",
                    "attempt_id": "build-1",
                    "reason": "timeout",
                },
            )
            assert refused.startswith("ERROR:")
        finally:
            release.set()
            assert await asyncio.to_thread(ended.wait, 10)

    asyncio.run(cancel_wait())
