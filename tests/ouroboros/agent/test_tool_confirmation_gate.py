from __future__ import annotations

import logging

import pytest

from new_core.llm.types import ToolCall, ToolCallFunction
from ouroboros.policy import safety
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import (
    collecting_event_sink,
    decision_for,
    execute_tool_round,
    make_catalog,
    make_static_tool_env,
    make_tool_call,
    make_tool_ctx,
    make_tool_entry,
    static_tool_provider,
)


@pytest.fixture(autouse=True)
def _safety_backend_is_not_what_these_test(monkeypatch):
    monkeypatch.setattr(safety, "check_safety", permissive_safety)


@parametrize_from_dict(
    {
        "approved": {
            "decisions": decision_for("data_write"),
            "ran": True,
            "head": "written",
            "status": "ok",
            "decision": "approved",
        },
        "denied": {
            "decisions": decision_for("data_write", verdict="denied"),
            "ran": False,
            "head": "⚠️ CONFIRMATION_DENIED",
            "status": "denied",
            "decision": "denied",
        },
        "nobody decided": {
            "decisions": {},
            "ran": False,
            "head": "⚠️ CONFIRMATION_NOT_ASKED",
            "status": "not_asked",
            "decision": "not_asked",
        },
    }
)
def test_a_gated_call_runs_on_an_approval_and_on_nothing_else(tmp_path, caplog, decisions, ran, head, status, decision):
    ran_calls: list[int] = []
    catalog, ctx = make_static_tool_env(
        tmp_path, make_tool_entry("data_write", lambda ctx, **kw: ran_calls.append(1) or "written")
    )
    events = collecting_event_sink()

    with caplog.at_level(logging.INFO, logger="ouroboros.agent.loop_tool_execution"):
        results = execute_tool_round(
            catalog, ctx, tmp_path, [make_tool_call("data_write")], events, decisions=decisions, confirmation_gate=True
        )

    assert results[0]["result"].startswith(head)
    assert bool(ran_calls) is ran
    assert [e["status"] for e in events.live_logs("tool_call_finished")] == [status]
    assert f"event=tool_confirmation_applied task_id=task1 tool=data_write decision={decision}" in caplog.messages


def test_an_approval_does_not_spread_to_a_sibling_sharing_its_call_id(tmp_path):
    """Some providers leave the id empty, so within one round it identifies nothing on its own."""
    ran: list[str] = []
    catalog, ctx = make_static_tool_env(
        tmp_path, make_tool_entry("data_write", lambda ctx, file_path="", **kw: ran.append(file_path) or "sent")
    )
    calls = [
        make_tool_call("data_write", {"file_path": "a.pdf"}),
        make_tool_call("data_write", {"file_path": "payroll.pdf"}),
    ]
    calls[0].id = calls[1].id = ""

    results = execute_tool_round(
        catalog,
        ctx,
        tmp_path,
        calls,
        collecting_event_sink(),
        decisions=decision_for("data_write", {"file_path": "a.pdf"}),
        confirmation_gate=True,
    )

    assert ran == ["a.pdf"]
    assert results[1]["result"].startswith("⚠️ CONFIRMATION_NOT_ASKED")


def test_the_gating_preflight_and_the_round_refuse_the_same_calls(tmp_path):
    """The owner is never asked about a call the round itself refuses."""
    from ouroboros.agent.loop_adapters import build_tool_call_executor

    class _BlockingGuard:
        def preflight(self, fn_name, args):
            return "⚠️ TOOL_REPEAT_BLOCKED" if args.get("file_path") == "looping.pdf" else ""

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    calls = [
        make_tool_call("data_write", {"file_path": "looping.pdf"}, idx=1),
        ToolCall(id="call-broken", function=ToolCallFunction(name="data_write", arguments="{not json")),
        make_tool_call("data_read", {"path": "notes.md"}, idx=2),
        make_tool_call("data_write", {"file_path": "fine.pdf"}, idx=3),
    ]
    executor = build_tool_call_executor(
        catalog=catalog,
        events=collecting_event_sink(),
        repeat_guard=_BlockingGuard(),
        confirmation_gate=True,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    gated = executor.gated_calls(calls, ctx=ctx, active_tool_names={"data_write"})
    results = executor.execute(calls, ctx=ctx, task_id="task1", active_tool_names={"data_write"}, decisions={})

    asked_about = {r["tool_call_id"] for r in results if r["result"].startswith("⚠️ CONFIRMATION_NOT_ASKED")}
    assert [calls[g.ordinal].id for g in gated] == ["call-3"]
    assert asked_about == {"call-3"}


def test_a_refusal_is_not_counted_as_a_tool_failure(tmp_path):
    """The owner declining is an outcome; counting it as a failure would block the tool for the task."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    events = collecting_event_sink()
    recorded: list[tuple] = []

    class _Guard:
        def preflight(self, fn_name, args):
            return ""

        def record_failure(self, fn_name, args, failure_class):
            recorded.append(("failure", fn_name))
            return ""

        def mark_success(self, fn_name, args):
            recorded.append(("success", fn_name))

    from ouroboros.agent.loop_adapters import build_tool_call_executor

    guard = _Guard()
    executor = build_tool_call_executor(
        catalog=catalog,
        events=events,
        repeat_guard=guard,
        confirmation_gate=True,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    results = executor.execute(
        [make_tool_call("data_write")], ctx=ctx, task_id="task1", decisions=decision_for("data_write", verdict="denied")
    )
    messages: list[dict] = []
    errors = executor.process_results(results, messages=messages, trace={"tool_calls": []}, ctx=ctx)

    assert errors == 0
    assert recorded == []


def test_a_gated_origin_is_asked_about_although_no_list_names_it(tmp_path):
    """A remote agent's tool name is generated per deployment, so only its origin can gate it."""
    catalog = make_catalog(
        [static_tool_provider(make_tool_entry("delegate_to_research", lambda ctx, **kw: "done"), origin="a2a:research")]
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

    results = execute_tool_round(
        catalog,
        ctx,
        tmp_path,
        [make_tool_call("delegate_to_research")],
        collecting_event_sink(),
        decisions=decision_for("delegate_to_research", verdict="denied"),
        confirmation_gate=True,
    )

    assert results[0]["result"].startswith("⚠️ CONFIRMATION_DENIED")


def test_a_remote_tool_resolved_only_at_dispatch_does_not_run_unasked(tmp_path):
    ran: list[int] = []
    provider = static_tool_provider(
        make_tool_entry("delegate_to_research", lambda ctx, **kw: ran.append(1) or "done"), origin="a2a:research"
    )
    lookups: list[str] = []
    resolve = provider.get

    def _connected_after_the_first_lookup(name, scope):
        lookups.append(name)
        return resolve(name, scope) if len(lookups) > 1 else None

    provider.get = _connected_after_the_first_lookup
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

    results = execute_tool_round(
        make_catalog([provider]),
        ctx,
        tmp_path,
        [make_tool_call("delegate_to_research")],
        collecting_event_sink(),
        confirmation_gate=True,
    )

    assert ran == []
    assert results[0]["result"].startswith("⚠️ TOOL_UNAVAILABLE")
